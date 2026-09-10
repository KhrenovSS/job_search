"""Collect client orders from the owner's profi.ru cabinet — one load of the orders feed per sitting.

Orders arrive with their full text, so they are stored straight into `prefiltered` (no details stage) and go to the
same AI evaluation as hh.ru vacancies, with profi-specific prompts. Strictly read-only: no responses, no clicks.
"""

from __future__ import annotations

import logging
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from hh_scout.browser import pacing
from hh_scout.browser.bursts import run_in_bursts
from hh_scout.browser.session import BrowserSession, BrowserUnavailable
from hh_scout.config import TZ, Settings
from hh_scout.pipeline import repo
from hh_scout.profi.pages import ProfiBlocked, parse_orders

log = logging.getLogger(__name__)


@dataclass
class ProfiStats:
    page_loads: int = 0
    orders_seen: int = 0
    new_orders: int = 0
    blocked: str | None = None

    def as_text(self) -> str:
        return (f"profi.ru: страниц {self.page_loads}, заказов в ленте {self.orders_seen}, новых {self.new_orders}"
                + (f", остановка: {self.blocked}" if self.blocked else ""))


class ProfiCollector:
    def __init__(self, settings: Settings, conn: sqlite3.Connection, *,
                 session_factory: Callable[[int], BrowserSession] | None = None, rng: random.Random | None = None,
                 gap_scale: float = 1.0, page_budget: int | None = None,
                 should_stop: Callable[[], bool] | None = None) -> None:
        self.s = settings
        self.conn = conn
        self.rng = rng or random.Random()
        self.gap_scale = gap_scale
        self.page_budget = page_budget if page_budget is not None else settings.profi_pages_per_run
        self.policy = pacing.policy_from_settings(settings)
        self._session_factory = session_factory or (lambda budget: BrowserSession(settings, page_budget=budget, rng=self.rng))
        self._should_stop = should_stop or (lambda: False)
        self.stats = ProfiStats()

    def _step(self, session: BrowserSession) -> bool:
        html = session.open_raw(self.s.profi_orders_url)
        orders = parse_orders(html, datetime.now(TZ))  # raises ProfiBlocked
        self.stats.orders_seen += len(orders)
        with self.conn:
            for o in orders:
                if repo.insert_order(self.conn, o):
                    self.stats.new_orders += 1
                    log.info("profi.ru: новый заказ %s «%s» (%s, %s, %s)", o.order_id, o.title[:50], o.city or "город не указан",
                             o.budget_text or "бюджет не указан", o.work_format)
        return False  # the feed is one page; more pages would mean more footprint on a site that forbids parsing

    def run(self, run_id: int | None = None) -> ProfiStats:
        if self.page_budget <= 0:
            return self.stats
        log.info("profi.ru: смотрю ленту заказов (%s), бюджет %d", self.s.profi_orders_url, self.page_budget)
        try:
            bs = run_in_bursts(self._step, session_factory=self._session_factory, budget=self.page_budget, policy=self.policy,
                               rng=self.rng, gap_scale=self.gap_scale, should_stop=self._should_stop, label="profi")
            self.stats.page_loads = bs.page_loads
        except BrowserUnavailable as e:
            self.stats.blocked = f"браузер недоступен: {e}"
            raise
        except ProfiBlocked as e:
            self.stats.blocked = str(e)
            log.warning("profi.ru: %s", e)
            raise
        log.info("%s", self.stats.as_text())
        return self.stats
