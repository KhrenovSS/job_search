import random

from hh_scout.browser import pacing


def test_page_delay_within_policy():
    p = pacing.PacingPolicy(page_delay_min_s=4, page_delay_max_s=12, long_read_every=8, long_read_min_s=20, long_read_max_s=40)
    rng = random.Random(1)
    delays = [pacing.page_delay(p, rng) for _ in range(500)]
    assert all(4 <= d <= 40 for d in delays)
    long = [d for d in delays if d >= 20]
    assert 20 < len(long) < 120  # roughly one in eight


def test_burst_and_gap_ranges():
    p = pacing.PacingPolicy()
    rng = random.Random(7)
    assert all(p.burst_min_s <= pacing.burst_duration(p, rng) <= p.burst_max_s for _ in range(100))
    assert all(p.gap_min_s <= pacing.gap_between_bursts(p, rng) <= p.gap_max_s for _ in range(100))


def test_policy_from_settings_reads_minutes_ranges():
    from hh_scout.config import Settings

    s = Settings(_env_file=None, burst_minutes="7-13", gap_minutes="4-9", page_delay_min_s=6, page_delay_max_s=20)
    p = pacing.policy_from_settings(s)
    assert (p.burst_min_s, p.burst_max_s) == (7 * 60, 13 * 60)
    assert (p.gap_min_s, p.gap_max_s) == (4 * 60, 9 * 60)
    assert (p.page_delay_min_s, p.page_delay_max_s) == (6, 20)


class _FakeDriver:
    def __init__(self):
        self.calls = []

    def execute_script(self, script, *args):
        self.calls.append((script, args))
        if "scrollHeight" in script:
            return 3000
        return None


def test_scroll_like_human_scrolls_without_sleeping(monkeypatch):
    monkeypatch.setattr(pacing, "sleep", lambda s: None)
    d = _FakeDriver()
    pacing.scroll_like_human(d, random.Random(3))
    scrolls = [c for c in d.calls if "scrollTo" in c[0]]
    assert len(scrolls) >= 2
    assert all(0 < c[1][0] <= 3000 for c in scrolls)
