"""hh.ru pages: URL builders and parsers of the embedded `HH-Lux-InitialState` JSON.

No network here. Parsers take the already-extracted state dict and return plain dataclasses;
`BrowserSession.open()` does the fetching. Unknown structures degrade to warnings, never crashes.

CLI (opens ONE page in the owner's Firefox):
    python -m hh_scout.browser.hh_pages --search CODESYS --area 1
    python -m hh_scout.browser.hh_pages --vacancy 136519902
"""

from __future__ import annotations

import html as html_mod
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urlencode

log = logging.getLogger(__name__)

BASE_URL = "https://hh.ru"
_STATE_RE = re.compile(r'<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<\s*(br|/p|/li|/div|/h\d|/tr)\s*/?>", re.I)
_WS_RE = re.compile(r"[ \t\xa0]+")
_NL_RE = re.compile(r"\n{3,}")


class PageFormatError(ValueError):
    """The page has no initial state or it lacks the expected section."""


# --- extraction ---------------------------------------------------------------

def extract_initial_state(page_source: str) -> dict[str, Any] | None:
    m = _STATE_RE.search(page_source)
    if not m:
        return None
    try:
        data = json.loads(html_mod.unescape(m.group(1)))
    except json.JSONDecodeError as e:
        log.warning("HH-Lux-InitialState не разобрался как JSON: %s", e)
        return None
    return data if isinstance(data, dict) else None


def user_type(state: dict[str, Any]) -> str:
    """'anonymous' when not logged in; 'applicant' for the owner's session."""
    return str(state.get("userType") or "unknown")


# --- URL builders -------------------------------------------------------------

def build_search_url(
    text: str,
    *,
    areas: Iterable[int] = (),
    work_formats: Iterable[str] = (),
    employment_forms: Iterable[str] = (),
    period_days: int = 2,
    page: int = 0,
    items_on_page: int = 50,
) -> str:
    params: list[tuple[str, str]] = [
        ("text", text),
        ("search_period", str(period_days)),
        ("items_on_page", str(items_on_page)),
        ("order_by", "publication_time"),
        ("no_magic", "true"),
    ]
    params += [("area", str(a)) for a in areas]
    params += [("work_format", w) for w in work_formats]
    params += [("employment_form", e) for e in employment_forms]
    if page:
        params.append(("page", str(page)))
    return f"{BASE_URL}/search/vacancy?{urlencode(params)}"


def vacancy_url(hh_id: str | int) -> str:
    return f"{BASE_URL}/vacancy/{hh_id}"


NEGOTIATIONS_URL = f"{BASE_URL}/applicant/negotiations?filter=all"


# --- normalisation ------------------------------------------------------------

_WORK_FORMAT_PRIORITY = ("REMOTE", "HYBRID", "ON_SITE", "FIELD_WORK")
_WORK_FORMAT_NAMES = {"REMOTE": "remote", "HYBRID": "hybrid", "ON_SITE": "office", "FIELD_WORK": "field"}
_EMPLOYMENT_NAMES = {"FULL": "full", "PART": "part", "PROJECT": "project", "SIDE_JOB": "part", "FLY_IN_FLY_OUT": "fly_in_fly_out"}


def normalize_work_format(raw: Any) -> str:
    """Accepts ['REMOTE'] or [{'workFormatsElement': ['ON_SITE', 'REMOTE']}]; picks the most flexible."""
    values: set[str] = set()
    for item in raw or []:
        if isinstance(item, str):
            values.add(item)
        elif isinstance(item, dict):
            values.update(v for v in item.get("workFormatsElement", []) if isinstance(v, str))
    for key in _WORK_FORMAT_PRIORITY:
        if key in values:
            return _WORK_FORMAT_NAMES[key]
    return "unknown"


def normalize_employment(raw: Any) -> str:
    if isinstance(raw, dict):
        raw = raw.get("@type") or raw.get("id")
    return _EMPLOYMENT_NAMES.get(str(raw or "").upper(), "unknown")


_TYPO_MAP = str.maketrans({"\xa0": " ", "\u2011": "-", "\u2010": "-", "\u2013": "-", "\u2014": "-", "\u00ad": ""})


def clean_text(value: Any) -> str:
    """Normalise typographic characters hh.ru inserts into titles (nbsp, non-breaking hyphens)."""
    return " ".join(str(value or "").translate(_TYPO_MAP).split())


def strip_html(fragment: str | None) -> str:
    if not fragment:
        return ""
    text = _BR_RE.sub("\n", fragment)
    text = _TAG_RE.sub(" ", text)
    text = html_mod.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _NL_RE.sub("\n\n", text).strip()


def _labels_mean_applied(labels: Iterable[Any]) -> bool:
    for label in labels or []:
        s = json.dumps(label, ensure_ascii=False).lower() if not isinstance(label, str) else label.lower()
        if "respon" in s or "отклик" in s or "negotiation" in s or "invitation" in s:
            return True
    return False


# --- search page --------------------------------------------------------------

@dataclass
class VacancyCard:
    hh_id: str
    title: str
    employer: str | None
    url: str
    area_name: str | None
    work_format: str
    employment: str
    compensation: dict | None
    published_at: str | None
    applied: bool = False
    archived: bool = False
    labels: list[str] = field(default_factory=list)
    employer_id: str | None = None  # hh.ru company.id — stable key for "one lead per company"


@dataclass
class SearchPage:
    total: int
    page: int
    has_next: bool
    cards: list[VacancyCard]
    user_type: str
    criteria: dict[str, Any]


def _company_name(company: Any) -> str | None:
    if not isinstance(company, dict):
        return None
    name = company.get("visibleName") or company.get("name")
    return clean_text(name) or None


def _company_id(company: Any) -> str | None:
    if not isinstance(company, dict) or company.get("id") in (None, ""):
        return None
    return str(company["id"])


def _card_from_raw(v: dict[str, Any], user_labels_map: dict[str, Any]) -> VacancyCard | None:
    vid = v.get("vacancyId") or v.get("id")
    if vid is None or not v.get("name"):
        return None
    vid = str(vid)
    labels = list(v.get("userLabels") or [])
    extra = user_labels_map.get(vid)
    if extra:
        labels += list(extra) if isinstance(extra, (list, tuple)) else [extra]
    pub = v.get("publicationTime")
    published_at = pub.get("$") if isinstance(pub, dict) else (pub if isinstance(pub, str) else None)
    links = v.get("links") if isinstance(v.get("links"), dict) else {}
    return VacancyCard(
        hh_id=vid,
        title=clean_text(v["name"]),
        employer=_company_name(v.get("company")),
        url=links.get("desktop") or vacancy_url(vid),
        area_name=(v.get("area") or {}).get("name") if isinstance(v.get("area"), dict) else None,
        work_format=normalize_work_format(v.get("workFormats")),
        employment=normalize_employment(v.get("employmentForm") or v.get("employment")),
        compensation=v.get("compensation") if isinstance(v.get("compensation"), dict) else None,
        published_at=published_at,
        applied=_labels_mean_applied(labels),
        archived=bool(v.get("@isArchived") or v.get("isArchived") or v.get("archived")),
        labels=[json.dumps(x, ensure_ascii=False) if not isinstance(x, str) else x for x in labels],
        employer_id=_company_id(v.get("company")),
    )


def _has_next(paging: Any, current: int) -> bool:
    if not isinstance(paging, dict):
        return False
    nxt = paging.get("next")
    if isinstance(nxt, dict):
        return not nxt.get("disabled", False)
    pages = paging.get("pages")
    if isinstance(pages, list):
        return any(isinstance(p, dict) and isinstance(p.get("page"), int) and p["page"] > current for p in pages)
    return False


def parse_search(state: dict[str, Any]) -> SearchPage:
    vsr = state.get("vacancySearchResult")
    if not isinstance(vsr, dict):
        raise PageFormatError("нет vacancySearchResult — это не страница поиска")
    criteria = vsr.get("criteria") if isinstance(vsr.get("criteria"), dict) else {}
    current = int(criteria.get("page") or 0)
    labels_map = state.get("userLabelsForVacancies")
    labels_map = {str(k): v for k, v in labels_map.items()} if isinstance(labels_map, dict) else {}
    cards: list[VacancyCard] = []
    for raw in vsr.get("vacancies") or []:
        if not isinstance(raw, dict):
            continue
        card = _card_from_raw(raw, labels_map)
        if card is None:
            log.warning("Карточка без id/названия пропущена: %s", str(raw)[:120])
            continue
        cards.append(card)
    return SearchPage(
        total=int(vsr.get("totalResults") or 0),
        page=current,
        has_next=_has_next(vsr.get("paging"), current),
        cards=cards,
        user_type=user_type(state),
        criteria=criteria,
    )


# --- vacancy page -------------------------------------------------------------

@dataclass
class VacancyDetail:
    hh_id: str
    title: str
    employer: str | None
    area_name: str | None
    work_format: str
    employment: str
    compensation: dict | None
    published_at: str | None
    description_html: str
    description_text: str
    key_skills: list[str]
    archived: bool
    applied: bool
    closed_for_applicants: bool
    raw: dict[str, Any]
    employer_id: str | None = None


def parse_vacancy(state: dict[str, Any]) -> VacancyDetail:
    vv = state.get("vacancyView")
    if not isinstance(vv, dict) or not vv.get("vacancyId"):
        raise PageFormatError("нет vacancyView — это не страница вакансии")
    status = vv.get("status") if isinstance(vv.get("status"), dict) else {}
    skills_raw = vv.get("keySkills")
    if isinstance(skills_raw, dict):
        skills_raw = skills_raw.get("keySkill")
    skills = [str(s) for s in (skills_raw or []) if s]
    desc_html = str(vv.get("description") or "")
    return VacancyDetail(
        hh_id=str(vv["vacancyId"]),
        title=clean_text(vv.get("name")),
        employer=_company_name(vv.get("company")),
        area_name=(vv.get("area") or {}).get("name") if isinstance(vv.get("area"), dict) else None,
        work_format=normalize_work_format(vv.get("workFormats")),
        employment=normalize_employment(vv.get("employmentForm")),
        compensation=vv.get("compensation") if isinstance(vv.get("compensation"), dict) else None,
        published_at=vv.get("publicationDate"),
        description_html=desc_html,
        description_text=strip_html(desc_html),
        key_skills=skills,
        archived=bool(status.get("archived")) or not status.get("active", True),
        applied=_labels_mean_applied(vv.get("userLabels") or []),
        closed_for_applicants=bool(vv.get("closedForApplicants")),
        raw=vv,
        employer_id=_company_id(vv.get("company")),
    )


# --- negotiations (owner's responses) ------------------------------------------

@dataclass
class NegotiationItem:
    hh_id: str
    state: str | None          # RESPONSE / INTERVIEW / DISCARD / ... (hh `lastState`)
    has_messages: bool         # employer wrote back (more than the owner's own response)
    title: str | None = None
    employer: str | None = None


def _short_vacancies_map(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """`vacanciesShort.vacanciesList` on the negotiations page: vacancy id → short card."""
    vs = state.get("vacanciesShort")
    items = vs.get("vacanciesList") if isinstance(vs, dict) else vs
    out: dict[str, dict[str, Any]] = {}
    for item in items or []:
        if isinstance(item, dict):
            vid = item.get("vacancyId") or item.get("id")
            if vid is not None:
                out[str(vid)] = item
    return out


def parse_negotiations(state: dict[str, Any]) -> list[NegotiationItem]:
    """Owner's responses from `https://hh.ru/applicant/negotiations?filter=all`.

    Verified shape (2026-09-08): `applicantNegotiations.topicList[]` with `vacancyId`, `lastState`,
    `conversationMessagesCount`, `hasNewMessages`, `archived`. Falls back to a generic walk if the
    section is missing, and returns [] with a warning if nothing looks like a negotiation.
    """
    short = _short_vacancies_map(state)
    found: dict[str, NegotiationItem] = {}

    an = state.get("applicantNegotiations")
    topics = an.get("topicList") if isinstance(an, dict) else None
    if isinstance(topics, list):
        for t in topics:
            if not isinstance(t, dict) or t.get("vacancyId") is None:
                continue
            vid = str(t["vacancyId"])
            msgs = int(t.get("conversationMessagesCount") or 0)
            card = short.get(vid, {})
            found[vid] = NegotiationItem(
                hh_id=vid,
                state=str(t.get("lastState") or t.get("initialState") or "") or None,
                has_messages=msgs > 1 or bool(t.get("hasNewMessages")),
                title=clean_text(card.get("name")) or None,
                employer=_company_name(card.get("company")),
            )
        return list(found.values())

    def visit(node: Any, depth: int = 0) -> None:
        if depth > 12:
            return
        if isinstance(node, dict):
            vid = node.get("vacancyId")
            if vid is None and isinstance(node.get("vacancy"), dict):
                vid = node["vacancy"].get("vacancyId") or node["vacancy"].get("id")
            if vid is not None and any(k in node for k in ("lastState", "state", "negotiationState", "status", "topicId", "negotiationId")):
                st = node.get("lastState") or node.get("state") or node.get("negotiationState") or node.get("status")
                st = st.get("id") if isinstance(st, dict) else st
                msgs = bool(node.get("hasNewMessages") or (node.get("messagesCount") or 0) > 1 or node.get("hasMessages"))
                found.setdefault(str(vid), NegotiationItem(str(vid), str(st) if st else None, msgs))
            for v in node.values():
                visit(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                visit(v, depth + 1)

    visit(state)
    if not found:
        log.warning("На странице откликов не найдено ни одной записи — структура могла измениться")
    return list(found.values())


def parse_suitable(state: dict[str, Any]) -> list[VacancyCard]:
    """Resume-based recommendations that hh.ru shows on the negotiations page (`suitableVacancies`)."""
    sv = state.get("suitableVacancies")
    if not isinstance(sv, dict):
        return []
    cards: list[VacancyCard] = []
    for raw in sv.get("vacancies") or []:
        if isinstance(raw, dict):
            card = _card_from_raw(raw, {})
            if card:
                cards.append(card)
    return cards


# --- CLI ------------------------------------------------------------------------

def _cli() -> int:
    import argparse
    import sys

    from hh_scout.browser.session import BrowserSession
    from hh_scout.config import load_settings
    from hh_scout.hh.salary import normalize
    from hh_scout.logging_setup import setup_logging

    ap = argparse.ArgumentParser(description="Open one hh.ru page in the owner's Firefox and print the parsed data")
    ap.add_argument("--search", help="search text (hh query language)")
    ap.add_argument("--area", type=int, action="append", default=[], help="area id, repeatable")
    ap.add_argument("--remote", action="store_true", help="work_format=REMOTE")
    ap.add_argument("--project", action="store_true", help="employment_form=PROJECT,PART")
    ap.add_argument("--page", type=int, default=0)
    ap.add_argument("--vacancy", help="vacancy id to open instead of a search")
    args = ap.parse_args()
    if not args.search and not args.vacancy:
        ap.error("нужен --search или --vacancy")

    settings = load_settings()
    setup_logging(settings.log_level)
    if args.vacancy:
        url = vacancy_url(args.vacancy)
    else:
        url = build_search_url(
            args.search,
            areas=args.area,
            work_formats=["REMOTE"] if args.remote else [],
            employment_forms=["PROJECT", "PART"] if args.project else [],
            period_days=settings.search_period_days,
            page=args.page,
            items_on_page=settings.items_per_page,
        )
    print("URL:", url)
    with BrowserSession(settings) as b:
        state = b.open(url)
        print("Пользователь на hh.ru:", user_type(state))
        if args.vacancy:
            d = parse_vacancy(state)
            print(f"{d.hh_id} | {d.title} | {d.employer} | {d.area_name} | {d.work_format}/{d.employment} | "
                  f"{normalize(d.compensation).human()} | архив={d.archived} | отклик={d.applied}")
            print("Навыки:", ", ".join(d.key_skills))
            print("Описание:", d.description_text[:600].replace("\n", " "), "…")
        else:
            sp = parse_search(state)
            print(f"Всего: {sp.total}, на странице: {len(sp.cards)}, есть следующая: {sp.has_next}")
            for i, c in enumerate(sp.cards, 1):
                print(f"{i:2d}. {c.hh_id} | {c.title[:60]} | {c.employer} | {c.area_name} | "
                      f"{c.work_format}/{c.employment} | {normalize(c.compensation).human()}"
                      f"{' | ОТКЛИК' if c.applied else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
