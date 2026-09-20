"""Human-like pacing for browsing hh.ru.

Two levels:
* inside a burst: a "reading" pause of several seconds after every page (occasionally a long one);
* between bursts: a few minutes of silence. A sitting is a handful of bursts — browse ~10 minutes, rest ~5,
  repeat — with every interval randomised, so the owner's account looks like a person reading vacancies
  in sittings during the day, never a metronome.

Every function takes an optional `rng` so tests stay deterministic.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from selenium.common.exceptions import WebDriverException

if TYPE_CHECKING:
    from hh_scout.config import Settings


@dataclass(frozen=True)
class PacingPolicy:
    page_delay_min_s: float = 6.0
    page_delay_max_s: float = 20.0
    long_read_every: int = 6          # roughly every N-th page gets a long "reading" pause
    long_read_min_s: float = 25.0
    long_read_max_s: float = 60.0
    burst_min_s: float = 7 * 60       # how long one burst of browsing lasts
    burst_max_s: float = 13 * 60
    gap_min_s: float = 4 * 60         # silence between bursts
    gap_max_s: float = 9 * 60


def policy_from_settings(settings: "Settings") -> PacingPolicy:
    burst_lo, burst_hi = settings.burst_seconds
    gap_lo, gap_hi = settings.gap_seconds
    return PacingPolicy(
        page_delay_min_s=settings.page_delay_min_s, page_delay_max_s=settings.page_delay_max_s,
        long_read_every=settings.long_read_every,
        long_read_min_s=settings.long_read_min_s, long_read_max_s=settings.long_read_max_s,
        burst_min_s=burst_lo, burst_max_s=burst_hi, gap_min_s=gap_lo, gap_max_s=gap_hi,
    )


def page_delay(policy: PacingPolicy, rng: random.Random | None = None) -> float:
    rng = rng or random
    if rng.random() < 1.0 / max(policy.long_read_every, 1):
        return rng.uniform(policy.long_read_min_s, policy.long_read_max_s)
    return rng.uniform(policy.page_delay_min_s, policy.page_delay_max_s)


def burst_duration(policy: PacingPolicy, rng: random.Random | None = None) -> float:
    rng = rng or random
    return rng.uniform(policy.burst_min_s, policy.burst_max_s)


def gap_between_bursts(policy: PacingPolicy, rng: random.Random | None = None) -> float:
    rng = rng or random
    return rng.uniform(policy.gap_min_s, policy.gap_max_s)


def sleep(seconds: float) -> None:
    """Indirection so tests can monkeypatch it."""
    time.sleep(seconds)


def monotonic() -> float:
    """Indirection so tests can monkeypatch it."""
    return time.monotonic()


def _wheel(driver, dy: int) -> bool:
    """Scroll with real wheel input events (what a person's mouse produces); False if the driver cannot."""
    try:
        from selenium.webdriver import ActionChains

        ActionChains(driver).scroll_by_amount(0, dy).perform()
        return True
    except Exception:  # noqa: BLE001 — older driver, hidden page, anything: fall back to a scripted scroll
        return False


def _nudge_mouse(driver, rng: random.Random) -> None:
    """A small pointer move, like a hand resting on the mouse. Out-of-viewport moves are simply skipped."""
    try:
        from selenium.webdriver import ActionChains

        ActionChains(driver).move_by_offset(int(rng.uniform(20, 160)), int(rng.uniform(20, 120))).perform()
    except Exception:  # noqa: BLE001
        pass


def scroll_like_human(driver, rng: random.Random | None = None) -> None:
    """Scroll down in a few uneven steps, sometimes a bit back up. Errors are ignored.

    Wheel events first (v9.11): `window.scrollTo` moves the page but fires no input events, and a session
    with hundreds of page views and not one wheel or pointer event is easy to tell from a person.
    """
    rng = rng or random
    try:
        height = int(driver.execute_script("return document.body.scrollHeight") or 0)
    except WebDriverException:
        return
    if height <= 0:
        return
    if rng.random() < 0.5:
        _nudge_mouse(driver, rng)
    target = rng.uniform(0.3, 0.9) * height
    pos = 0.0
    scripted = False
    while pos < target:
        step = rng.uniform(250, 700)
        pos += step
        if scripted or not _wheel(driver, int(step)):
            scripted = True
            try:
                driver.execute_script("window.scrollTo(0, arguments[0]);", int(pos))
            except WebDriverException:
                return
        sleep(rng.uniform(0.3, 1.2))
    if rng.random() < 0.3:
        back = -int(rng.uniform(100, 400))
        if scripted or not _wheel(driver, back):
            try:
                driver.execute_script("window.scrollBy(0, arguments[0]);", back)
            except WebDriverException:
                return
