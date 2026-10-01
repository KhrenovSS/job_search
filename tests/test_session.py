"""`BrowserSession.open()` against a fake WebDriver: no geckodriver, no Firefox, no network (v9.39)."""
import random

import pytest

from hh_scout.browser import pacing
from hh_scout.browser.session import BrowserSession, HHBlocked, PageBudgetExceeded, PageIncomplete
from hh_scout.config import Settings

URL = "https://hh.ru/search/vacancy?text=PLC&page=1"
FULL = '<html><head><title>Работа в Москве - hh.ru</title></head><body><template id="HH-Lux-InitialState">{"userType": "applicant", "x": "ok"}</template></body></html>'
CUT = '<html><head><title>Работа в Москве - hh.ru</title></head><body><template id="HH-Lux-InitialState">{"userType": "applicant", "x": "o</template></body></html>'
NO_TEMPLATE = '<html><head><title>Проверка</title></head><body>captcha</body></html>'


class FakeDriver:
    """Serves `sources` one per `page_source` read; `readyState` goes interactive → complete between reads."""

    def __init__(self, sources):
        self.sources = list(sources)
        self.reads = 0
        self.refreshes = 0
        self.title = "Работа в Москве - hh.ru"
        self.current_url = URL
        self.window_handles = ["w1"]
        self.current_window_handle = "w1"
        self.switch_to = self

    def window(self, h):
        pass

    @property
    def page_source(self):
        self.reads += 1
        return self.sources[min(self.reads, len(self.sources)) - 1]

    def execute_script(self, script, *args):
        if "readyState" in script and "indexOf" not in script:
            return "complete" if self.reads else "interactive"
        if "indexOf" in script:
            return True
        if "scrollHeight" in script:
            return 0
        return None

    def set_page_load_timeout(self, t):
        pass

    def get(self, url):
        self.current_url = url

    def refresh(self):
        self.refreshes += 1


def _session(monkeypatch, sources, budget=5):
    monkeypatch.setattr(pacing, "sleep", lambda s: None)
    monkeypatch.setattr(pacing, "scroll_like_human", lambda d, rng=None: None)
    s = BrowserSession(Settings(_env_file=None), page_budget=budget, rng=random.Random(0))
    s._driver = FakeDriver(sources)
    s._own_window = "w1"
    return s


def test_open_rereads_a_page_whose_state_was_cut_short(monkeypatch):
    """Sitting #106 (01.10): the search page was read mid-transfer, the JSON ended inside a string at 419 KB,
    and the whole sitting stopped as a «captcha». Now the page is read again once the document is complete."""
    s = _session(monkeypatch, [CUT, FULL])
    assert s.open(URL) == {"userType": "applicant", "x": "ok"}
    assert s.page_loads == 1 and s.driver.refreshes == 0


def test_open_reloads_once_when_the_page_stays_broken(monkeypatch):
    s = _session(monkeypatch, [CUT, CUT, FULL])
    assert s.open(URL)["x"] == "ok"
    assert s.page_loads == 2 and s.driver.refreshes == 1


def test_open_gives_up_after_one_reload(monkeypatch):
    s = _session(monkeypatch, [CUT, CUT, CUT])
    with pytest.raises(PageIncomplete) as e:
        s.open(URL)
    assert s.page_loads == 2 and s.driver.refreshes == 1 and e.value.url == URL


def test_open_does_not_reload_without_budget(monkeypatch):
    s = _session(monkeypatch, [CUT, CUT, FULL], budget=1)
    with pytest.raises(PageIncomplete):
        s.open(URL)
    assert s.page_loads == 1 and s.driver.refreshes == 0


def test_open_still_raises_a_block_without_the_template(monkeypatch):
    s = _session(monkeypatch, [NO_TEMPLATE])
    s.driver.title = "Проверка"
    with pytest.raises(HHBlocked) as e:
        s.open(URL)
    assert e.value.title == "Проверка" and s.driver.refreshes == 0


def test_open_respects_the_budget_before_the_first_load(monkeypatch):
    s = _session(monkeypatch, [FULL], budget=0)
    with pytest.raises(PageBudgetExceeded):
        s.open(URL)
