import json
import random
from pathlib import Path

import pytest

from hh_scout.browser import pacing
from hh_scout.browser.session import PageBudgetExceeded
from hh_scout.config import Settings
from hh_scout.db import connect, migrate
from hh_scout.pipeline import repo
from hh_scout.pipeline.profi_collector import ProfiCollector
from hh_scout.profi.pages import ProfiBlocked

FIXTURES = Path(__file__).parent / "fixtures"


class FakeSession:
    html = ""
    opened: list[str] = []

    def __init__(self, budget):
        self.page_budget, self.page_loads = budget, 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def open_raw(self, url):
        if self.page_loads >= self.page_budget:
            raise PageBudgetExceeded("b")
        self.page_loads += 1
        FakeSession.opened.append(url)
        return FakeSession.html


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(pacing, "sleep", lambda s: None)
    FakeSession.opened = []
    FakeSession.html = (FIXTURES / "profi_orders.html").read_text(encoding="utf-8")
    conn = connect(":memory:")
    migrate(conn)
    return conn


def _settings(**kw):
    return Settings(_env_file=None, profi_enabled=True, **kw)


def test_orders_land_as_prefiltered_rows_and_are_not_duplicated(db):
    s = _settings()
    c = ProfiCollector(s, db, session_factory=FakeSession, rng=random.Random(0), page_budget=1)
    st = c.run()
    assert st.page_loads == 1 and st.orders_seen == 2 and st.new_orders == 2
    assert FakeSession.opened == [s.profi_orders_url]
    rows = repo.list_vacancies(db, "prefiltered")
    assert {r["hh_id"] for r in rows} == {"profi:93716145", "profi:93638725"}
    r = next(r for r in rows if r["hh_id"] == "profi:93716145")
    assert r["site"] == "profi" and r["source"] == "profi" and r["search_pass"] == "profi"
    assert r["employer"] == "Клиент А" and r["area_name"] == "Москва" and r["work_format"] == "remote"
    assert r["employment"] == "project" and r["salary_to"] == 5000 and r["salary_from"] is None
    raw = json.loads(r["raw_json"])
    assert raw["description"].startswith("Имеется готовая программа") and raw["budget"] == "до 5000 ₽"
    assert json.loads(r["salary_raw"])["profi_budget"] == "до 5000 ₽"
    assert r["url"] == "https://profi.ru/backoffice/n.php?o=93716145"
    # second sitting: same feed -> nothing new, still one page load
    st2 = ProfiCollector(s, db, session_factory=FakeSession, rng=random.Random(0), page_budget=1).run()
    assert st2.new_orders == 0 and st2.orders_seen == 2 and repo.count_site(db, "profi") == 2


def test_login_page_raises_profi_blocked(db):
    FakeSession.html = "<html><body><h1>Вход</h1>Введите номер телефона</body></html>"
    c = ProfiCollector(_settings(), db, session_factory=FakeSession, rng=random.Random(0), page_budget=1)
    with pytest.raises(ProfiBlocked):
        c.run()
    assert c.stats.blocked and repo.count_site(db, "profi") == 0


def test_zero_budget_means_no_browsing(db):
    c = ProfiCollector(_settings(), db, session_factory=FakeSession, rng=random.Random(0), page_budget=0)
    assert c.run().page_loads == 0 and FakeSession.opened == []
