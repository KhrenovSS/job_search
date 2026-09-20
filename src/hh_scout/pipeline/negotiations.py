"""Read the owner's own responses on hh.ru and record what the companies did (decisions #42, #48).

One place for both callers: a sitting reads `NEGOTIATIONS_PAGES` pages at the start of its collection burst
(`Collector.run`), and `scripts/sync_negotiations.py` walks the whole list once to backfill history. Both go
through `run_in_bursts` (pauses, budget, `should_stop`) and both count their pages in `runs` — the daily cap is
shared by every process that touches hh.ru (CLAUDE.md rule 3). Read-only, like everything else on the site.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable

from hh_scout.browser.hh_pages import VacancyCard, negotiations_has_next, negotiations_url, parse_negotiations, parse_suitable, user_type
from hh_scout.browser.session import BrowserSession
from hh_scout.db import transaction
from hh_scout.pipeline import repo

log = logging.getLogger(__name__)

PASS_SIMILAR = "similar"
SOURCE_SIMILAR = "similar_to_resume"

StoreCards = Callable[[list[VacancyCard], str, str], int]   # (cards, source, search_pass) -> how many were new


@dataclass
class NegotiationsSync:
    """Cursor and tallies of one walk over the responses list."""

    pages: int                       # how many list pages to read at most
    page: int = 0                    # next page to load (0-based)
    done: bool = False
    seen: set[str] = field(default_factory=set)
    synced: int = 0                  # distinct responses recorded in this walk
    similar_new: int = 0             # resume-based recommendations hh shows on the first page
    user_type: str | None = None     # 'applicant' when logged in; anything else means the responses are invisible

    @property
    def not_logged_in(self) -> bool:
        return self.user_type is not None and self.user_type != "applicant"


def sync_page(conn: sqlite3.Connection, session: BrowserSession, sync: NegotiationsSync,
              store_cards: StoreCards | None = None) -> None:
    """Load one page of the list and record every response on it.

    The cursor advances *before* the load: a page interrupted by the budget or a blocked site is skipped,
    not re-read blind on the next burst — the sync runs again in a few hours anyway.
    """
    page = sync.page
    sync.page = page + 1
    state = session.open(negotiations_url(page))
    items = parse_negotiations(state)
    with transaction(conn):
        for n in items:
            repo.mark_applied(conn, n.hh_id, has_chat=n.has_messages, state=n.state, title=n.title, employer=n.employer)
    ids = {n.hh_id for n in items}
    fresh = ids - sync.seen
    sync.synced += len(fresh)
    if page == 0:   # the recommendations block and the login check live on the first page only
        sync.user_type = user_type(state)
        if sync.not_logged_in:
            log.warning("hh.ru видит нас как %r — вход в аккаунт в Firefox не выполнен, отклики не видны", sync.user_type)
        similar = parse_suitable(state)
        if store_cards is not None and similar:
            sync.similar_new = store_cards(similar, SOURCE_SIMILAR, PASS_SIMILAR)
        log.info("Отклики, страница 1: %d; подходящих по резюме: %d, новых %d", len(items), len(similar), sync.similar_new)
    else:
        log.info("Отклики, страница %d: %d, из них новых для этого подхода %d", page + 1, len(items), len(fresh))
    if items and not fresh:
        # hh ignored `page=` and served the same list again — stop after one wasted load
        log.warning("Список откликов повторился на странице %d — параметр page= не работает, листать перестаю", page + 1)
    sync.seen |= ids
    sync.done = (not items or not fresh or page + 1 >= sync.pages or not negotiations_has_next(state, page))
    if sync.done:
        log.info("Отклики: синхронизировано %d за %d стр.", sync.synced, page + 1)


def walk(conn: sqlite3.Connection, settings: Any, *, session_factory: Callable[[int], BrowserSession], pages: int,
         budget: int, rng: Any, gap_scale: float = 1.0, should_stop: Callable[[], bool] | None = None,
         after_burst: Callable[[Any], None] | None = None) -> tuple[NegotiationsSync, Any]:
    """Walk the responses list on its own, in bursts — for the backfill script. Returns (sync, BurstStats)."""
    from hh_scout.browser import pacing
    from hh_scout.browser.bursts import run_in_bursts

    sync = NegotiationsSync(pages=pages)

    def step(session: BrowserSession) -> bool:
        if not sync.done:
            sync_page(conn, session, sync)
        return not sync.done

    stats = run_in_bursts(step, session_factory=session_factory, budget=budget, policy=pacing.policy_from_settings(settings),
                          rng=rng, gap_scale=gap_scale, should_stop=should_stop, after_burst=after_burst, label="отклики")
    return sync, stats
