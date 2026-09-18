"""Nightly database backup (decision #40) and the markup wait that replaces the 60 s timeout (#39)."""

import sqlite3

from hh_scout import db as dbmod
from hh_scout.browser import session as session_mod
from hh_scout.browser.hh_pages import HH_STATE_MARKER
from hh_scout.config import Settings


def test_backup_copies_the_live_db_and_keeps_only_the_newest(tmp_path):
    conn = dbmod.connect(tmp_path / "live.db")
    dbmod.migrate(conn)
    conn.execute("INSERT INTO kv(key, value) VALUES ('probe', 'kept')")

    dest = tmp_path / "backups"
    target = dbmod.backup(conn, dest, keep=2)
    assert target.exists()
    copy = sqlite3.connect(str(target))
    assert copy.execute("SELECT value FROM kv WHERE key='probe'").fetchone()[0] == "kept"
    copy.close()

    # older copies beyond `keep` are pruned, the newest survive
    for day in ("2026-09-01", "2026-09-02", "2026-09-03"):
        (dest / f"hh_scout-{day}.db").write_bytes(b"old")
    dbmod.backup(conn, dest, keep=2)
    left = sorted(p.name for p in dest.glob("hh_scout-*.db"))
    assert len(left) == 2
    assert target.name in left  # today's copy is the newest and is never the one pruned


class _FakeDriver:
    """Returns the page without the marker for the first `misses` polls, then with it."""

    def __init__(self, misses: int, marker: str = HH_STATE_MARKER) -> None:
        self.misses = misses
        self.marker = marker
        self.calls = 0

    def execute_script(self, script, *args):
        self.calls += 1
        if self.calls <= self.misses:
            return "<html><body>грузится</body></html>"
        return f'<html><template id="{self.marker}">{{}}</template></html>'


def _session(driver):
    s = session_mod.BrowserSession(Settings())
    s._driver = driver
    return s


def test_markup_wait_returns_as_soon_as_the_marker_is_there(monkeypatch):
    slept = []
    monkeypatch.setattr(session_mod.time, "sleep", lambda s: slept.append(s))
    d = _FakeDriver(misses=0)
    assert _session(d)._wait_for_markers((HH_STATE_MARKER,)) is True
    assert d.calls == 1 and slept == []  # server-rendered markup: no waiting at all


def test_markup_wait_polls_until_the_page_settles(monkeypatch):
    monkeypatch.setattr(session_mod.time, "sleep", lambda s: None)
    d = _FakeDriver(misses=3)
    assert _session(d)._wait_for_markers((HH_STATE_MARKER,)) is True
    assert d.calls == 4


def test_markup_wait_gives_up_after_the_deadline(monkeypatch):
    monkeypatch.setattr(session_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(session_mod, "MARKUP_WAIT_S", 0.0)
    assert _session(_FakeDriver(misses=99))._wait_for_markers((HH_STATE_MARKER,)) is False


def test_page_load_timeout_is_short_enough_to_not_burn_a_minute():
    assert session_mod.PAGE_LOAD_TIMEOUT_S <= 20
