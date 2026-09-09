import random

from hh_scout.browser import pacing
from hh_scout.browser.bursts import run_in_bursts
from hh_scout.browser.session import PageBudgetExceeded


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeSession:
    def __init__(self, budget):
        self.page_budget, self.page_loads = budget, 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def open(self):
        if self.page_loads >= self.page_budget:
            raise PageBudgetExceeded("b")
        self.page_loads += 1


def _run(monkeypatch, *, work, budget, step_seconds=60.0):
    clock = FakeClock()
    monkeypatch.setattr(pacing, "monotonic", clock.monotonic)
    monkeypatch.setattr(pacing, "sleep", clock.sleep)
    policy = pacing.PacingPolicy(burst_min_s=600, burst_max_s=600, gap_min_s=300, gap_max_s=300)
    left = {"n": work}
    sizes = []

    def step(session):
        session.open()
        clock.sleep(step_seconds)  # one page takes a minute of "reading"
        left["n"] -= 1
        return left["n"] > 0

    stats = run_in_bursts(step, session_factory=FakeSession, budget=budget, policy=policy, rng=random.Random(0),
                          after_burst=lambda bs: sizes.append(bs.page_loads))
    return stats, sizes, clock, left


def test_burst_is_time_boxed_then_pauses(monkeypatch):
    stats, sizes, clock, left = _run(monkeypatch, work=25, budget=100)
    # 10-minute bursts at one page per minute -> 10 pages per burst, 3 bursts, two 5-minute gaps
    assert stats.bursts == 3 and stats.page_loads == 25 and left["n"] == 0
    assert sizes == [10, 20, 25]
    assert clock.t == 25 * 60 + 2 * 300
    assert stats.stopped_reason is None


def test_budget_stops_the_loop(monkeypatch):
    stats, sizes, clock, left = _run(monkeypatch, work=50, budget=15)
    assert stats.page_loads == 15 and stats.stopped_reason == "исчерпан дневной лимит загрузок"
    assert left["n"] == 35


def test_should_stop_ends_burst_and_loop(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(pacing, "monotonic", clock.monotonic)
    monkeypatch.setattr(pacing, "sleep", clock.sleep)
    policy = pacing.PacingPolicy(burst_min_s=600, burst_max_s=600, gap_min_s=1, gap_max_s=1)
    calls = {"n": 0}

    def step(session):
        session.open()
        calls["n"] += 1
        return True

    stats = run_in_bursts(step, session_factory=FakeSession, budget=100, policy=policy, rng=random.Random(0),
                          should_stop=lambda: calls["n"] >= 3)
    assert calls["n"] == 3 and stats.stopped_reason == "остановлено"
