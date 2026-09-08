"""Human-like pacing for browsing hh.ru.

Two levels:
* inside a burst: a pause of a few seconds after every page (occasionally a long "reading" pause);
* between bursts: tens of minutes. A daily crawl is a handful of small bursts spread over the
  crawl window, never one continuous sweep — the owner's account must look like a person reading
  vacancies over an afternoon.

Every function takes an optional `rng` so tests stay deterministic.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

from selenium.common.exceptions import WebDriverException


@dataclass(frozen=True)
class PacingPolicy:
    page_delay_min_s: float = 4.0
    page_delay_max_s: float = 12.0
    long_read_every: int = 8          # roughly every N-th page gets a long "reading" pause
    long_read_min_s: float = 20.0
    long_read_max_s: float = 40.0
    burst_min_pages: int = 3          # pages per burst
    burst_max_pages: int = 7
    gap_min_s: float = 10 * 60        # pause between bursts
    gap_max_s: float = 40 * 60


def page_delay(policy: PacingPolicy, rng: random.Random | None = None) -> float:
    rng = rng or random
    if rng.random() < 1.0 / max(policy.long_read_every, 1):
        return rng.uniform(policy.long_read_min_s, policy.long_read_max_s)
    return rng.uniform(policy.page_delay_min_s, policy.page_delay_max_s)


def burst_size(policy: PacingPolicy, rng: random.Random | None = None) -> int:
    rng = rng or random
    return rng.randint(policy.burst_min_pages, policy.burst_max_pages)


def gap_between_bursts(policy: PacingPolicy, rng: random.Random | None = None) -> float:
    rng = rng or random
    return rng.uniform(policy.gap_min_s, policy.gap_max_s)


def sleep(seconds: float) -> None:
    """Indirection so tests can monkeypatch it."""
    time.sleep(seconds)


def scroll_like_human(driver, rng: random.Random | None = None) -> None:
    """Scroll down in a few uneven steps, sometimes a bit back up. Errors are ignored."""
    rng = rng or random
    try:
        height = int(driver.execute_script("return document.body.scrollHeight") or 0)
    except WebDriverException:
        return
    if height <= 0:
        return
    target = rng.uniform(0.3, 0.9) * height
    pos = 0.0
    while pos < target:
        pos += rng.uniform(250, 700)
        try:
            driver.execute_script("window.scrollTo(0, arguments[0]);", int(pos))
        except WebDriverException:
            return
        sleep(rng.uniform(0.3, 1.2))
    if rng.random() < 0.3:
        try:
            driver.execute_script("window.scrollBy(0, arguments[0]);", -int(rng.uniform(100, 400)))
        except WebDriverException:
            return
