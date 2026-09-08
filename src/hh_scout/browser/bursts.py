"""Run browser work in short bursts separated by long pauses (shared by collector and details)."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Callable

from hh_scout.browser import pacing
from hh_scout.browser.session import BrowserSession, PageBudgetExceeded

log = logging.getLogger(__name__)

Step = Callable[[BrowserSession], bool]  # do one unit of work; return True if more work remains


@dataclass
class BurstStats:
    page_loads: int = 0
    bursts: int = 0
    stopped_reason: str | None = None


def run_in_bursts(
    step: Step,
    *,
    session_factory: Callable[[int], BrowserSession],
    budget: int,
    policy: pacing.PacingPolicy,
    rng: random.Random,
    gap_scale: float = 1.0,
    should_stop: Callable[[], bool] | None = None,
    after_burst: Callable[[BurstStats], None] | None = None,
    label: str = "",
) -> BurstStats:
    """Call `step` repeatedly inside bursts until it reports no more work or the budget is spent."""
    stats = BurstStats()
    more = True
    while more:
        if should_stop and should_stop():
            stats.stopped_reason = "остановлено"
            break
        remaining = budget - stats.page_loads
        if remaining <= 0:
            stats.stopped_reason = "исчерпан дневной лимит загрузок"
            break
        size = min(pacing.burst_size(policy, rng), remaining)
        stats.bursts += 1
        log.info("%sСерия %d: до %d страниц (осталось в бюджете %d)", f"[{label}] " if label else "", stats.bursts, size, remaining)
        with session_factory(size) as session:
            try:
                while more:
                    more = step(session)
            except PageBudgetExceeded:
                pass  # burst budget spent; continue after a gap
            finally:
                stats.page_loads += session.page_loads
        if after_burst:
            after_burst(stats)
        if more:
            gap = pacing.gap_between_bursts(policy, rng) * gap_scale
            log.info("%sПауза между сериями %.0f мин", f"[{label}] " if label else "", gap / 60)
            pacing.sleep(gap)
    return stats
